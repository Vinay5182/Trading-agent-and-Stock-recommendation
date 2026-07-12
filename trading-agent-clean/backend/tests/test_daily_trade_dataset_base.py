import asyncio
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from pymongo.errors import DuplicateKeyError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.daily_dataset_contract import (
    ACTION_PENDING,
    LABEL_PENDING,
    LIFECYCLE_NOT_STARTED,
    OUTCOME_NOT_READY,
    PAPER_LINK_PENDING,
    STAGE_SCORE_SNAPSHOT,
    STRATEGY_MOMENTUM,
    STRATEGY_SWING,
    TV_STATUS_PENDING,
)
from services import mongo_indexes
from services.daily_dataset import (
    build_candidate_key,
    build_daily_dataset_candidate_snapshot_run,
    build_daily_dataset_rows_from_scored_candidates,
    build_dataset_id,
    pre_decision_model_input,
)


FIXED_AUDIT_TIME = "2026-07-08T10:30:00.000000Z"
SOURCE_CANDLE_AT = "2026-07-08T10:00:00+00:00"
SECOND_AUDIT_TIME = "2026-07-08T10:45:00.000000Z"


def run(coro):
    return asyncio.run(coro)


def nested_get(row: dict, dotted_key: str):
    current = row
    for part in dotted_key.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def nested_set(row: dict, dotted_key: str, value):
    current = row
    parts = dotted_key.split(".")
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = deepcopy(value)


def matches_query(row: dict, query: dict | None) -> bool:
    if not query:
        return True
    for key, expected in query.items():
        if key == "$or":
            if not any(matches_query(row, option) for option in expected):
                return False
            continue
        actual = nested_get(row, key)
        if actual != expected:
            return False
    return True


class FakeCursor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = [deepcopy(row) for row in rows]
        self.index = 0

    def sort(self, *args):
        sort_keys = args[0] if args and isinstance(args[0], list) else [args[:2]]
        for key, direction in reversed(sort_keys):
            self.rows.sort(key=lambda row: nested_get(row, key) or "", reverse=direction < 0)
        return self

    def limit(self, limit: int):
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


class FakeDailyDatasetCollection:
    def __init__(self, rows: list[dict] | None = None, *, duplicate_once: bool = False) -> None:
        self.rows = [deepcopy(row) for row in rows or []]
        self.update_calls = []
        self.duplicate_once = duplicate_once

    def find(self, query: dict | None = None, *_args, **_kwargs):
        return FakeCursor([row for row in self.rows if matches_query(row, query)])

    async def update_one(self, query: dict, update: dict, upsert: bool = False):
        self.update_calls.append((deepcopy(query), deepcopy(update), upsert))
        if self.duplicate_once and upsert:
            self.duplicate_once = False
            raise DuplicateKeyError("duplicate identity.dataset_id")

        row = next((candidate for candidate in self.rows if matches_query(candidate, query)), None)
        if row is not None:
            for key, value in update.get("$set", {}).items():
                nested_set(row, key, value)
            return SimpleNamespace(matched_count=1, modified_count=1 if update.get("$set") else 0, upserted_id=None)

        if not upsert:
            return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=None)

        new_row = {}
        for key, value in query.items():
            if not key.startswith("$"):
                nested_set(new_row, key, value)
        for key, value in update.get("$setOnInsert", {}).items():
            nested_set(new_row, key, value)
        for key, value in update.get("$set", {}).items():
            nested_set(new_row, key, value)
        new_row.setdefault("_id", f"id-{len(self.rows) + 1}")
        self.rows.append(new_row)
        return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=new_row["_id"])


class FakeDailyDatasetDb:
    def __init__(self, scored_rows: list[dict], *, duplicate_once: bool = False) -> None:
        self.scored_candidates = FakeDailyDatasetCollection(scored_rows)
        self.daily_trade_dataset = FakeDailyDatasetCollection(duplicate_once=duplicate_once)
        self.dataset_build_runs = FakeDailyDatasetCollection([])

    def __getitem__(self, name: str):
        return getattr(self, name)


def scored_candidate(**overrides):
    row = {
        "_id": "scored-1",
        "exchange": "nse",
        "symbol": "TEST",
        "canonical_symbol": "TEST",
        "tradingview_symbol": "NSE:TEST",
        "index_name": "BROAD_MARKET_750",
        "current_price": 101.5,
        "previous_close": 99.0,
        "open_price": 100.0,
        "day_high": 103.0,
        "day_low": 98.5,
        "traded_volume": 123456,
        "traded_value": 12345678,
        "change_percent": 2.52,
        "relative_volume": 1.8,
        "thirty_day_change_percent": 12.4,
        "source_used": "test",
        "updated_at": SOURCE_CANDLE_AT,
        "score_version": "score_v1",
        "score": 83.0,
        "selected_for_tv": True,
        "swing_candidate": True,
        "swing_status": "SWING_SELECTED_FOR_TV",
        "momentum_score": 74.0,
        "momentum_candidate": True,
        "momentum_status": "MOMENTUM_PRECHECK_PASSED",
        "score_breakdown": {
            "swing": {"trend": 30, "volume": 20},
            "momentum": {"momentum": 32, "volume": 25},
        },
    }
    row.update(overrides)
    return row


def dataset_id_kwargs(**overrides):
    kwargs = {
        "trade_date": "2026-07-08",
        "exchange": "NSE",
        "canonical_symbol": "TEST",
        "strategy_type": "momentum",
        "candidate_key": "candidate-key",
        "source_candle_at_utc": SOURCE_CANDLE_AT,
        "timeframe_set_hash": None,
    }
    kwargs.update(overrides)
    return kwargs


def test_deterministic_dataset_id_same_input_gives_same_output():
    first = build_dataset_id(**dataset_id_kwargs())
    second = build_dataset_id(**dataset_id_kwargs())

    assert first == second
    assert first.startswith("dtd_v1_")
    assert len(first) == len("dtd_v1_") + 32


def test_dataset_id_changes_when_identity_parts_change():
    base = build_dataset_id(**dataset_id_kwargs())
    changed_ids = {
        build_dataset_id(**dataset_id_kwargs(canonical_symbol="OTHER")),
        build_dataset_id(**dataset_id_kwargs(strategy_type="SWING")),
        build_dataset_id(**dataset_id_kwargs(trade_date="2026-07-09")),
        build_dataset_id(**dataset_id_kwargs(candidate_key="other-key")),
    }

    assert base not in changed_ids
    assert len(changed_ids) == 4


def test_candidate_key_uses_setup_id_first():
    row = scored_candidate(setup_id="setup-primary", candidate_id="candidate-secondary")

    assert build_candidate_key(row, strategy_type=STRATEGY_MOMENTUM) == "setup-primary"


def test_candidate_key_uses_candidate_id_second():
    row = scored_candidate(candidate_id="candidate-secondary")

    assert build_candidate_key(row, strategy_type=STRATEGY_MOMENTUM) == "candidate-secondary"


def test_candidate_key_deterministic_fallback_works():
    row = scored_candidate(_id="scored-fallback", candidate_id=None, setup_id=None, scan_run_id="scan-1")

    first = build_candidate_key(row, strategy_type=STRATEGY_MOMENTUM)
    second = build_candidate_key(dict(row), strategy_type=STRATEGY_MOMENTUM)
    swing = build_candidate_key(dict(row), strategy_type=STRATEGY_SWING)

    assert first == second
    assert first.startswith("candidate_")
    assert first != swing


def test_dry_run_builder_creates_momentum_row_for_momentum_candidate():
    rows = build_daily_dataset_rows_from_scored_candidates(
        [scored_candidate(swing_candidate=False, selected_for_tv=False)],
        trade_date="2026-07-08",
        strategy_type=STRATEGY_MOMENTUM,
        audit_time=FIXED_AUDIT_TIME,
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["identity"]["strategy_type"] == STRATEGY_MOMENTUM
    assert row["score_snapshot"]["strategy_score"] == 74.0
    assert row["score_snapshot"]["candidate_status"] == "MOMENTUM_PRECHECK_PASSED"


def test_dry_run_builder_creates_swing_row_for_swing_candidate():
    rows = build_daily_dataset_rows_from_scored_candidates(
        [scored_candidate(momentum_candidate=False)],
        trade_date="2026-07-08",
        strategy_type=STRATEGY_SWING,
        audit_time=FIXED_AUDIT_TIME,
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["identity"]["strategy_type"] == STRATEGY_SWING
    assert row["score_snapshot"]["strategy_score"] == 83.0
    assert row["score_snapshot"]["candidate_status"] == "SWING_SELECTED_FOR_TV"


def test_both_candidate_stock_creates_two_rows():
    rows = build_daily_dataset_rows_from_scored_candidates(
        [scored_candidate()],
        trade_date="2026-07-08",
        audit_time=FIXED_AUDIT_TIME,
    )

    assert len(rows) == 2
    assert {row["identity"]["strategy_type"] for row in rows} == {STRATEGY_SWING, STRATEGY_MOMENTUM}
    assert len({row["identity"]["dataset_id"] for row in rows}) == 2


def test_default_placeholders_are_correct():
    row = build_daily_dataset_rows_from_scored_candidates(
        [scored_candidate(momentum_candidate=False)],
        trade_date="2026-07-08",
        strategy_type=STRATEGY_SWING,
        audit_time=FIXED_AUDIT_TIME,
    )[0]

    assert row["identity"]["current_stage"] == STAGE_SCORE_SNAPSHOT
    assert row["identity"]["is_current"] is True
    assert row["tv_confirmation_snapshot"]["status"] == TV_STATUS_PENDING
    assert row["decision_snapshot"]["action"] == ACTION_PENDING
    assert row["paper_trade_link"]["link_status"] == PAPER_LINK_PENDING
    assert row["lifecycle_snapshot"]["lifecycle_status"] == LIFECYCLE_NOT_STARTED
    assert row["future_outcome"]["outcome_state"] == OUTCOME_NOT_READY
    assert row["ml_label"]["label_state"] == LABEL_PENDING


def test_leakage_sensitive_fields_are_not_in_pre_decision_feature_snapshot():
    row = build_daily_dataset_rows_from_scored_candidates(
        [scored_candidate(momentum_candidate=False)],
        trade_date="2026-07-08",
        strategy_type=STRATEGY_SWING,
        audit_time=FIXED_AUDIT_TIME,
    )[0]

    features = pre_decision_model_input(row)
    serialized = str(features).lower()

    assert set(features) == {"raw_market_snapshot", "score_snapshot", "strategy_selection"}
    for blocked in ("future_outcome", "ml_label", "paper_pnl", "target_1_hit", "sl_hit", "label_category"):
        assert blocked not in serialized


def test_index_definitions_include_daily_trade_dataset_indexes():
    daily_specs = {spec.name: spec for spec in mongo_indexes.get_collection_index_specs("daily_trade_dataset")}
    build_run_specs = {spec.name: spec for spec in mongo_indexes.get_collection_index_specs("dataset_build_runs")}

    assert daily_specs["daily_trade_dataset_dataset_id_unique"].unique is True
    assert daily_specs["daily_trade_dataset_dataset_id_unique"].create_keys() == [("identity.dataset_id", 1)]
    assert daily_specs["daily_trade_dataset_identity_hash_version_unique"].unique is True
    assert daily_specs["daily_trade_dataset_paper_trade_id_unique"].unique is True
    assert daily_specs["daily_trade_dataset_paper_trade_id_unique"].sparse is True
    assert "daily_trade_dataset_label_export" in daily_specs
    assert "daily_trade_dataset_build_id" in daily_specs

    assert build_run_specs["dataset_build_runs_build_id_unique"].unique is True
    assert build_run_specs["dataset_build_runs_build_id_unique"].create_keys() == [("dataset_build_id", 1)]
    assert "dataset_build_runs_trade_date_from_status" in build_run_specs
    assert "dataset_build_runs_schema_build_started" in build_run_specs


def test_persistent_build_inserts_candidate_rows_and_manifest():
    db = FakeDailyDatasetDb([scored_candidate(momentum_candidate=False)])

    result = run(
        build_daily_dataset_candidate_snapshot_run(
            db,
            trade_date="2026-07-08",
            strategy_type=STRATEGY_SWING,
            dry_run=False,
            dataset_build_id="build-1",
            audit_time=FIXED_AUDIT_TIME,
        )
    )

    assert result["status"] == "COMPLETED"
    assert result["built_count"] == 1
    assert result["inserted_count"] == 1
    assert result["updated_count"] == 0
    assert len(db.daily_trade_dataset.rows) == 1
    stored = db.daily_trade_dataset.rows[0]
    assert nested_get(stored, "identity.dataset_id")
    assert nested_get(stored, "raw_market_snapshot.current_price") == 101.5
    assert nested_get(stored, "score_snapshot.strategy_score") == 83.0
    assert nested_get(stored, "audit_metadata.dry_run") is False

    assert len(db.dataset_build_runs.rows) == 1
    manifest = db.dataset_build_runs.rows[0]
    assert manifest["dataset_build_id"] == "build-1"
    assert manifest["status"] == "COMPLETED"
    assert manifest["inserted_count"] == 1


def test_second_persistent_build_does_not_duplicate_rows():
    db = FakeDailyDatasetDb([scored_candidate(momentum_candidate=False)])

    run(
        build_daily_dataset_candidate_snapshot_run(
            db,
            trade_date="2026-07-08",
            strategy_type=STRATEGY_SWING,
            dry_run=False,
            dataset_build_id="build-1",
            audit_time=FIXED_AUDIT_TIME,
        )
    )
    db.scored_candidates.rows = [scored_candidate(momentum_candidate=False)]
    result = run(
        build_daily_dataset_candidate_snapshot_run(
            db,
            trade_date="2026-07-08",
            strategy_type=STRATEGY_SWING,
            dry_run=False,
            dataset_build_id="build-2",
            audit_time=SECOND_AUDIT_TIME,
        )
    )

    assert result["inserted_count"] == 0
    assert result["updated_count"] == 1
    assert len(db.daily_trade_dataset.rows) == 1
    assert nested_get(db.daily_trade_dataset.rows[0], "audit_metadata.last_build_id") == "build-2"


def test_immutable_market_and_score_snapshots_are_not_overwritten_on_rerun():
    db = FakeDailyDatasetDb([scored_candidate(momentum_candidate=False, current_price=101.5, score=83.0)])

    run(
        build_daily_dataset_candidate_snapshot_run(
            db,
            trade_date="2026-07-08",
            strategy_type=STRATEGY_SWING,
            dry_run=False,
            dataset_build_id="build-1",
            audit_time=FIXED_AUDIT_TIME,
        )
    )
    db.scored_candidates.rows = [
        scored_candidate(momentum_candidate=False, current_price=999.0, score=1.0, source_used="changed-provider")
    ]
    run(
        build_daily_dataset_candidate_snapshot_run(
            db,
            trade_date="2026-07-08",
            strategy_type=STRATEGY_SWING,
            dry_run=False,
            dataset_build_id="build-2",
            audit_time=SECOND_AUDIT_TIME,
        )
    )

    stored = db.daily_trade_dataset.rows[0]
    assert nested_get(stored, "raw_market_snapshot.current_price") == 101.5
    assert nested_get(stored, "raw_market_snapshot.source_used") == "test"
    assert nested_get(stored, "score_snapshot.strategy_score") == 83.0
    assert nested_get(stored, "audit_metadata.last_seen_at") == SECOND_AUDIT_TIME
    assert nested_get(stored, "provenance.dataset_build_id") == "build-2"


def test_both_candidate_stock_creates_two_persistent_rows():
    db = FakeDailyDatasetDb([scored_candidate()])

    result = run(
        build_daily_dataset_candidate_snapshot_run(
            db,
            trade_date="2026-07-08",
            dry_run=False,
            dataset_build_id="build-both",
            audit_time=FIXED_AUDIT_TIME,
        )
    )

    assert result["built_count"] == 2
    assert result["inserted_count"] == 2
    assert len(db.daily_trade_dataset.rows) == 2
    assert {nested_get(row, "identity.strategy_type") for row in db.daily_trade_dataset.rows} == {
        STRATEGY_SWING,
        STRATEGY_MOMENTUM,
    }


def test_duplicate_key_handling_does_not_crash_the_build():
    db = FakeDailyDatasetDb([scored_candidate(momentum_candidate=False)], duplicate_once=True)

    result = run(
        build_daily_dataset_candidate_snapshot_run(
            db,
            trade_date="2026-07-08",
            strategy_type=STRATEGY_SWING,
            dry_run=False,
            dataset_build_id="build-duplicate",
            audit_time=FIXED_AUDIT_TIME,
        )
    )

    assert result["status"] == "COMPLETED"
    assert result["inserted_count"] == 0
    assert result["duplicate_skipped_count"] == 1
    assert result["error_count"] == 0
    assert len(db.daily_trade_dataset.rows) == 0


def test_dry_run_build_does_not_write_rows_or_manifest():
    db = FakeDailyDatasetDb([scored_candidate(momentum_candidate=False)])

    result = run(
        build_daily_dataset_candidate_snapshot_run(
            db,
            trade_date="2026-07-08",
            strategy_type=STRATEGY_SWING,
            dry_run=True,
            dataset_build_id="dry-run-build",
            audit_time=FIXED_AUDIT_TIME,
        )
    )

    assert result["status"] == "DRY_RUN"
    assert result["mongo_writes"] is False
    assert result["built_count"] == 1
    assert len(db.daily_trade_dataset.rows) == 0
    assert len(db.dataset_build_runs.rows) == 0


def test_historical_candidates_can_build_daily_dataset_dry_run():
    from backend.services.daily_dataset import build_daily_dataset_candidate_snapshot_run
    
    historical_row = {
        "historical_candidate_id": "hsc_123",
        "canonical_symbol": "TEST",
        "trade_date": "2026-05-20",
        "strategy_type": "MULTI",
        "selected_for_tv": True,
        "momentum_candidate": True,
        "swing_status": "SWING_SELECTED_FOR_TV",
        "momentum_status": "MOMENTUM_PRECHECK_PASSED",
        "score": 90,
        "momentum_score": 95,
        "normalized_score_inputs": {"relative_volume": 2.0},
    }
    
    mock_db = {}
    mock_collection = type("MockCollection", (), {})()
    
    class MockCursor:
        def __init__(self, items):
            self.items = items
        def sort(self, *args, **kwargs):
            return self
        async def to_list(self, length):
            return self.items
            
    def mock_find(query):
        return MockCursor([historical_row])
        
    setattr(mock_collection, "find", mock_find)
    mock_db["historical_scored_candidates"] = mock_collection
    
    result = run(
        build_daily_dataset_candidate_snapshot_run(
            mock_db,
            trade_date="2026-05-20",
            limit=100,
            dry_run=True,
            source_collection_name="historical_scored_candidates",
        )
    )
    
    assert result["dry_run"] is True
    assert result["mongo_writes"] is False
    assert result["source"] == "historical_scored_candidates"
    assert result["built_count"] == 2
    assert result["strategy_counts"]["SWING"] == 1
    assert result["strategy_counts"]["MOMENTUM"] == 1
    
    rows = result["rows"]
    assert len(rows) == 2
    swing_row = next(r for r in rows if r["identity"]["strategy_type"] == "SWING")
    mom_row = next(r for r in rows if r["identity"]["strategy_type"] == "MOMENTUM")
    
    assert swing_row["identity"]["trade_date"] == "2026-05-20"
    assert swing_row["identity"]["dataset_id"].startswith("dtd_v1_")
    assert mom_row["identity"]["dataset_id"].startswith("dtd_v1_")
    assert swing_row["identity"]["dataset_id"] != mom_row["identity"]["dataset_id"]

